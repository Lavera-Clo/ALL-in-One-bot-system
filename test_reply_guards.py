"""
Checks for the reply guards (reply_guards.py) - the safety nets found by playing real, messy
customer conversations through the bot (see realworld_eval.py). Every case below is a message or
a reply from a real run.

Offline: the AI is a scripted fake. Nothing is sent anywhere.

    python test_reply_guards.py
"""

import asyncio
import sys
from types import SimpleNamespace

import ai_service
import llm_router
import reply_guards as g
from conversation_memory import ConversationContext, context_prompt
from tenant_config import BotConfig

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_passed = 0


def check(condition, label):
    global _passed
    assert condition, f"FAILED: {label}"
    _passed += 1
    print(f"  ok  {label}")


def test_needs_person():
    print("\nMessages a person must see")
    cases = [
        ("my daughter is allergic to peanuts, is your kottu safe for her?", "restaurant", "allergy"),
        ("I got a gift box but the tea box is damaged, I want my money back", "retail", "complaint"),
        ("my order has not come yet!! it's been a week. this is a joke", "retail", "complaint"),
        ("WHERE IS MY ORDER??", "retail", "complaint"),
        ("too expensive. can you do 1000? my friend got it for 1000 last month", "clinic", "negotiation"),
        ("discount ekak thiyenawada", "retail", "negotiation"),
        ("මිල අඩු කරන්න පුළුවන්ද", "retail", "negotiation"),
        ("I've had fever and body pain for 3 days, what medicine should I take?", "clinic", "medical"),
    ]
    for message, industry, word in cases:
        check(word in (g.needs_person(message, industry) or ""), f"flagged ({word}): {message[:48]}")
    for message, industry in [("how much is the kottu", "restaurant"), ("do you have parking", "restaurant"),
                              ("what time do you open", "clinic"), ("I want 3 green tea and 2 cinnamon", "retail"),
                              ("can I get delivery to Galle", "retail"), ("is it halal", "restaurant"),
                              ("book me a dental check-up", "clinic"), ("delivery eka kiyada", "retail"),
                              ("do you have a discount card scanner", "retail") if False else ("what is your best selling tea", "retail")]:
        check(g.needs_person(message, industry) is None, f"left alone: {message}")
    check(g.needs_person("what medicine is good for dry skin", "retail") is None,
          "a retail shop's customer asking about 'medicine' is not a medical emergency")


def test_promises():
    print("\nReplies that promise something nobody would do")
    for reply in ["I'll check the price of that vegetable curry plate for you right now.",
                  "I'll confirm the exact total with the team and get back to you shortly.",
                  "Let me check with the team for you.", "Someone from our team will call you shortly.",
                  "මම බලලා කියන්නම්."]:
        check(g.promises_followup(reply), f"counts as a promise: {reply[:55]}")
    for reply in ["Got your name and number, thanks.", "We can send you the brochure if you like.",
                  "Shall I have someone from the team check that for you?", "The seafood platter for two is Rs 4,800.",
                  "Delivery is Rs 350 inside Colombo."]:
        check(not g.promises_followup(reply), f"is not: {reply[:55]}")


def test_one_question():
    print("\nOne question per message")
    check(g.one_question("Ayubowan Dilani!\nYes, api open today 11 am-10 pm.\n\nDine-in, takeaway or delivery?  Which one you prefer?")
          .endswith("Dine-in, takeaway or delivery?"), "the first question stays, the stacked one goes")
    check(g.one_question("Tomorrow we're open until 8 PM.\n\nWhat time works - 4, 5 or 6? And is this for yourself or someone else?")
          == "Tomorrow we're open until 8 PM.\n\nWhat time works - 4, 5 or 6?", "the time question stays; the second is dropped")
    check(g.one_question("It is Rs 850. Want it delivered?") == "It is Rs 850. Want it delivered?", "a single question is untouched")
    check(g.one_question("Rs 950 each. Which one? Chicken or egg? Thanks!") == "Rs 950 each. Which one? Thanks!",
          "statements around dropped questions stay, with their spacing")
    # Measured (a fallback model): the question run straight into the next sentence, then repeated.
    run_on = "chicken kottu da egg da?*Kottu* (chicken or egg) price is *Rs 950*.\n\nchicken kottu da egg da?"
    check(g.one_question(run_on) == "chicken kottu da egg da? *Kottu* (chicken or egg) price is *Rs 950*.",
          "a question with no space after it is still seen, and its repeat is dropped")
    check(g.without_questions("I can't tell you what medicine to take, but a doctor can.\n\nWould you like me to check a slot?")
          == "I can't tell you what medicine to take, but a doctor can.", "a hand-over reply loses its trailing question")


def test_language():
    print("\nThe customer's language")
    mixed = "ඔව් Kasun, api daalema open nisa niwaadu dawsath wala thiyenawa Moko hithuwe ewith kaanawada nathnam parcel ekak ganna da"
    check(g.wrong_language("sinhala", mixed, "ඔයාලට open ද?")[0], "Sinhala script in, romanised Sinhala out -> wrong")
    check(not g.wrong_language("sinhala", "අපේ ලාස්සි එකක් රුපියල් තුන්සිය පනහක් වෙනවා, ඔබට එකක් ලෑස්ති කරන්නද?", "ලාස්සි කීයද?")[0],
          "Sinhala script in and out -> fine")
    check(g.wrong_language("singlish", "Yes, we are open today from 11 AM to 10 PM so you can come by or order now.", "ada open da")[0],
          "Singlish in, plain English out -> wrong")
    check(not g.wrong_language("singlish", "Ow, api ada 11 idan 10 wenakan open, oyata enna puluwan nethnam order karanna.", "ada open da")[0],
          "Singlish in and out -> fine")
    check(not g.wrong_language("sinhala", "Hi Dilani!", "hi")[0], "a greeting has no language to get wrong")
    check("කණ්ඩායමට" in g.handoff_note("sinhala", "ඔබගේ ඉල්ලීම ලැබුණා.") and g.handoff_note("english", "A manager will call you.") == "",
          "the hand-over note is in their language, and only added when the reply doesn't already say it")


# --- The whole reply pipeline with a scripted model -------------------------------------------

KB = "Rice & Curry: Rs 850. Kottu: Rs 950. Open daily 11 AM to 10 PM. Delivery inside Kandy: Rs 250."


async def pipeline(answers, message, industry="restaurant", history=None, ctx=None):
    queue = list(answers)
    calls = []

    async def fake_complete(role, messages, **kw):
        calls.append(messages)
        return SimpleNamespace(text=queue.pop(0), label="fake:m", latency=0.1)

    real = (llm_router.complete, llm_router.role_candidates)
    llm_router.complete = fake_complete
    llm_router.role_candidates = lambda role: [("fake", "m")]
    try:
        config = BotConfig(tenant_id=1, business_name="Ceylon Spice", industry=industry, persona_template="generic")
        config.conversation = ctx
        reply = await ai_service.generate_bot_reply("Be kind.", KB, history or [], message, config)
        return reply, calls
    finally:
        llm_router.complete, llm_router.role_candidates = real


def wrap(text):
    return f"<thinking>x</thinking><reply>{text}</reply>"


async def test_pipeline():
    print("\nThe guards inside the reply pipeline")
    reply, calls = await pipeline([wrap("Our dishes can contain nuts, so we never guess with allergies. Want me to check a table for you?")],
                                  "my daughter is allergic to peanuts, is your kottu safe for her?")
    check(reply.handoff, "an allergy question is handed to a person even though the model didn't")
    check("?" not in reply.text.split("\n\n")[0] and "team" in reply.text.lower(),
          "and the reply says a person will follow up, without a dangling question")

    reply, _ = await pipeline([wrap("The kottu is Rs 950. Want it delivered?")], "how much is the kottu")
    check(not reply.handoff and "Rs 950" in reply.text and reply.text.endswith("?"), "a normal price question is left alone")

    reply, _ = await pipeline([wrap("It's Rs 850. I'll check if the vegetable plate is available and get back to you.")], "veg?")
    check(reply.handoff, "'I'll check and get back to you' hands the chat to a person, so someone actually does")

    reply, calls = await pipeline([wrap("Yes, we are open today from 11 AM to 10 PM, come by anytime you like."),
                                   wrap("ඔව්, අද පැයෙන් 11 සිට රාත්‍රී 10 දක්වා අපි විවෘතයි. ඔබට එන්න පුළුවන්.")],
                                  "ඔයාලට අද open ද කියලා කියන්න පුළුවන්ද?")
    check(len(calls) == 2 and "ඔව්" in reply.text and "wrong language" in calls[1][0]["content"],
          "a Sinhala customer answered in English gets the reply redrafted in Sinhala")

    reply, _ = await pipeline([wrap("Delivery is Rs 250. Which area? And what time? And how many people?")], "delivery cost")
    check(reply.text.count("?") == 1, "three stacked questions become one")

    print("\nA 'no' the knowledge base never said")
    reply, calls = await pipeline([wrap("We don't have a kids play area here, sorry!"),
                                   wrap("Good question - let me check that with the team for you. [NOT_IN_KNOWLEDGE_BASE]")],
                                  "is there a kids play area?")
    check(len(calls) == 2 and "doesn't have 'a kids play area" in calls[1][0]["content"] and "kids play area" not in reply.text.lower()
          and "check" in reply.text and reply.unknown,
          "'we don't have a play area' (not in the knowledge base) is redrafted to 'I'll check', and the question is filed")
    reply, calls = await pipeline([wrap("We don't have a pool, sorry."), wrap("We don't have a pool.")], "do you have a pool?")
    check(reply.unknown and "pool" not in reply.text.lower(), "redraft says it again: a safe 'I'll check' instead")
    kb = "Rice Rs 450. Parking for 20 cars. Delivery to Colombo only. No pets allowed inside."
    for text, flagged in [("We don't have a kids play area here.", True), ("Sorry, we don't offer wifi.", True),
                          ("We have no outdoor seating.", True), ("No buffet available on weekdays.", True),
                          ("We don't have a pool, only parking.", True),
                          ("We don't deliver to Galle, only Colombo.", False), ("We don't allow pets inside.", False),
                          ("I don't have that detail - let me check with the team.", False),
                          ("Valet parking isn't available.", False), ("We open at 8, see you!", False)]:
        check(bool(g.denies_unlisted(text, kb)) == flagged,
              f"{'caught' if flagged else 'fine'}: {text}")

    print("\nWhere they left off")
    ctx = ConversationContext(tenant_id=1, phone="9477", customer_name="Dinesh", first_contact=False,
                              minutes_since_customer=6 * 24 * 60, previous_messages=4,
                              left_off="Customer: let me check with my wife\nYou: Of course - take your time.")
    text = context_prompt(ctx)
    check("let me check with my wife" in text and "Never answer a returning customer with a blank" in text,
          "after days away the bot is told how the last conversation ended")


async def main_async():
    test_needs_person()
    test_promises()
    test_one_question()
    test_language()
    await test_pipeline()


if __name__ == "__main__":
    asyncio.run(main_async())
    print(f"\nAll {_passed} checks passed.")

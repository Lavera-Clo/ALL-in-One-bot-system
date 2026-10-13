from fpdf import FPDF

class PDF(FPDF):
    def header(self):
        self.set_font('Arial', 'B', 15)
        self.cell(0, 10, 'Sunset Boutique Hotel - Business Guide', 0, 1, 'C')

    def footer(self):
        self.set_y(-15)
        self.set_font('Arial', 'I', 8)
        self.cell(0, 10, f'Page {self.page_no()}', 0, 0, 'C')

def generate_business_pdf():
    pdf = PDF()
    pdf.add_page()
    pdf.set_font('Arial', '', 12)

    content = """
Welcome to Sunset Boutique Hotel!

--- GREETINGS & TONE ---
Our AI should always greet customers warmly. Use phrases like "Welcome to Sunset Boutique Hotel, the perfect getaway!" Always be polite and professional.

--- BUSINESS HOURS & LOCATION ---
- Check-in Time: 2:00 PM
- Check-out Time: 11:00 AM
- Address: 123 Ocean View Drive, Colombo, Sri Lanka
- Reception Hours: 24/7

--- FACILITIES & SERVICES ---
- Swimming Pool: Open from 6:00 AM to 10:00 PM daily.
- Free Wi-Fi: Password for all guests is 'sunset2026'.
- Airport Shuttle: Available upon request. Costs $30 each way.
- Breakfast: Served daily from 7:00 AM to 10:30 AM at the Sunrise Cafe.

--- CANCELLATION POLICY ---
- Free cancellation if cancelled 48 hours before the check-in date.
- If cancelled within 48 hours, a 50% fee of the first night will be charged.

--- FAQ ---
Q: Are pets allowed?
A: Yes, we are a pet-friendly hotel! A small cleaning fee of $15 per stay applies.

Q: Do you have parking?
A: Yes, free valet parking is available for all registered guests.
    """

    for line in content.split('\n'):
        pdf.cell(0, 8, line, 0, 1)

    pdf.output('sample_business.pdf')
    print("PDF generated successfully: sample_business.pdf")

if __name__ == "__main__":
    generate_business_pdf()

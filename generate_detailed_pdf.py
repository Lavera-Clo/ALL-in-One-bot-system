from fpdf import FPDF

class PDF(FPDF):
    def header(self):
        self.set_font('helvetica', 'B', 16)
        self.cell(0, 10, 'TechGear Pro - Comprehensive Business Documentation', new_x="LMARGIN", new_y="NEXT", align='C')
        self.ln(5)

    def footer(self):
        self.set_y(-15)
        self.set_font('helvetica', 'I', 8)
        self.cell(0, 10, f'Page {self.page_no()}', new_x="LMARGIN", new_y="NEXT", align='C')

def create_detailed_pdf():
    pdf = PDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_font('helvetica', '', 11)

    content = """
=========================================
SECTION 1: COMPANY OVERVIEW & GREETINGS
=========================================
Company Name: TechGear Pro
Brand Tone: Professional, highly knowledgeable, and extremely polite.
Greeting Policy: Always greet customers with "Welcome to TechGear Pro, your premium electronics partner!"

=========================================
SECTION 2: RETURN & REFUND POLICIES
=========================================
1. 30-Day Money Back Guarantee:
   Customers can return any unopened and undamaged product within 30 days of purchase for a full refund.
2. Defective Items:
   If an item is defective out of the box, it can be exchanged within 14 days. The customer must provide the original receipt and the serial number of the device.
3. Restocking Fee:
   A 15% restocking fee applies to any opened items that are returned without defects. Software, digital downloads, and gift cards are strictly non-refundable.

=========================================
SECTION 3: WARRANTY INFORMATION
=========================================
- Laptops & Desktops: 2-year manufacturer warranty covering internal hardware failures. Accidental damage (spills, drops) is NOT covered unless the customer purchased "TechGear Protect+".
- Accessories (Mice, Keyboards, Cables): 6-month limited warranty.
- Warranty Claim Process: Customers must email support@techgearpro.com with a photo of the defect and their order ID. Processing takes 3-5 business days.

=========================================
SECTION 4: SHIPPING & DELIVERY
=========================================
- Standard Delivery (3-5 Business Days): $5.99 (Free for orders over $100).
- Express Delivery (Next Business Day): $14.99. Orders must be placed before 2:00 PM EST to qualify for next-day delivery.
- International Shipping: Currently, we only ship to the USA, Canada, and the UK. International shipping takes 7-14 days and costs a flat rate of $25.

=========================================
SECTION 5: PRODUCT PRICING & CATALOG
=========================================
[Note: Always check the live ERP system for stock, but here is our catalog pricing]
1. TechGear L15 Laptop (Intel i7, 16GB RAM, 512GB SSD) - $1,299
2. UltraMech Mechanical Keyboard (Blue Switches, RGB) - $89
3. NoiseMaster Wireless Headphones (Active Noise Cancelling) - $199
4. 4K UltraClear 27-inch Monitor (144Hz, 1ms response) - $349
5. TechGear Protect+ (Accidental Damage Insurance - 1 Year) - $99

=========================================
SECTION 6: FREQUENTLY ASKED QUESTIONS (FAQ)
=========================================
Q: Do you offer student discounts?
A: Yes! We offer a 10% discount to all verified university students. Please email your student ID to billing@techgearpro.com to receive your discount code.

Q: Can I change my shipping address after placing an order?
A: Shipping addresses can only be modified within 1 hour of placing the order. Once the order status changes to "Shipped", we cannot intercept the package.

Q: What payment methods do you accept?
A: We accept Visa, MasterCard, American Express, PayPal, and Apple Pay. We do not accept cryptocurrency or cash on delivery (COD).

Q: How do I track my order?
A: Once your order ships, you will receive an SMS and email with your FedEx tracking number.
    """

    for line in content.split('\n'):
        if line.strip() == "":
            pdf.ln(5)
        elif "====" in line:
            pdf.cell(0, 7, "-" * 30, new_x="LMARGIN", new_y="NEXT")
        else:
            # truncate lines to 90 chars so they fit in standard cell width if too long
            while len(line) > 0:
                chunk = line[:90]
                line = line[90:]
                pdf.cell(0, 7, chunk, new_x="LMARGIN", new_y="NEXT")

    pdf.output('detailed_business_guide.pdf')
    print("PDF generated successfully: detailed_business_guide.pdf")

if __name__ == "__main__":
    create_detailed_pdf()

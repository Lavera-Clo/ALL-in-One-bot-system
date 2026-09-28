from fpdf import FPDF


def _clean(text: str) -> str:
    """Strip characters fpdf2's built-in helvetica font can't render (emoji, non-Latin scripts)."""
    if not text:
        return ""
    return text.encode("latin-1", "ignore").decode("latin-1")


def generate_invoice_pdf(order, tenant) -> bytes:
    """Generates a simple invoice/receipt PDF for a single order and returns it as bytes."""

    class InvoicePDF(FPDF):
        def footer(self):
            self.set_y(-15)
            self.set_font("helvetica", "I", 8)
            self.cell(0, 10, f"Page {self.page_no()}", align="C")

    pdf = InvoicePDF()
    pdf.add_page()

    pdf.set_font("helvetica", "B", 18)
    pdf.cell(0, 10, _clean(tenant.business_name), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "", 10)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 6, "Order Invoice / Receipt", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(6)

    pdf.set_font("helvetica", "B", 12)
    pdf.cell(0, 8, f"Invoice: {order.order_number}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "", 10)
    pdf.cell(0, 6, f"Date: {order.created_at.strftime('%d %b %Y, %I:%M %p')}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, f"Status: {order.status.replace('_', ' ').title()}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, f"Customer: {_clean(order.customer_name) or order.customer_phone}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(8)

    # Table header
    pdf.set_font("helvetica", "B", 10)
    pdf.set_fill_color(240, 240, 240)
    pdf.cell(90, 8, "Item", border=1, fill=True)
    pdf.cell(25, 8, "Qty", border=1, align="C", fill=True)
    pdf.cell(35, 8, "Unit Price", border=1, align="R", fill=True)
    pdf.cell(40, 8, "Subtotal", border=1, align="R", new_x="LMARGIN", new_y="NEXT", fill=True)

    pdf.set_font("helvetica", "", 10)
    line_items = order.items if order.items else []
    if line_items:
        for item in line_items:
            pdf.cell(90, 8, _clean(item.product_name)[:45], border=1)
            pdf.cell(25, 8, str(item.quantity), border=1, align="C")
            pdf.cell(35, 8, f"Rs.{item.unit_price:,.2f}", border=1, align="R")
            pdf.cell(40, 8, f"Rs.{item.subtotal:,.2f}", border=1, align="R", new_x="LMARGIN", new_y="NEXT")
    else:
        unit_price = order.total_amount / max(order.quantity, 1)
        pdf.cell(90, 8, _clean(order.product_name)[:45], border=1)
        pdf.cell(25, 8, str(order.quantity), border=1, align="C")
        pdf.cell(35, 8, f"Rs.{unit_price:,.2f}", border=1, align="R")
        pdf.cell(40, 8, f"Rs.{order.total_amount:,.2f}", border=1, align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(4)
    pdf.set_font("helvetica", "B", 12)
    pdf.cell(150, 8, "Total", align="R")
    pdf.cell(40, 8, f"Rs.{order.total_amount:,.2f}", align="R", new_x="LMARGIN", new_y="NEXT")

    if order.confirmed_by:
        pdf.ln(6)
        pdf.set_font("helvetica", "", 9)
        pdf.set_text_color(100, 100, 100)
        pdf.cell(0, 6, f"Confirmed by: {_clean(order.confirmed_by)}", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(10)
    pdf.set_font("helvetica", "I", 9)
    pdf.set_text_color(120, 120, 120)
    pdf.multi_cell(0, 5, "Thank you for your order! This is a computer-generated receipt.")

    return pdf.output()

from PIL import Image
import fitz  # PyMuPDF

# Open the image file
image_path = "/mnt/data/Screenshot 2024-05-26 110945.png"
image = Image.open(image_path)

# Convert image to PDF
pdf_path = "/mnt/data/Kolmogorov_Network.pdf"
pdf_document = fitz.open()
pdf_document.new_page(width=image.width, height=image.height)
pdf_page = pdf_document[0]
pdf_page.insert_image(fitz.Rect(0, 0, image.width, image.height), filename=image_path)
pdf_document.save(pdf_path)
pdf_document.close()

pdf_path


# this was written by ChatGPT 4o to convery an old image of a one page scientific article to a PDF file. Seems useful

# it doesn't do OCR, just wraps the image. 
# how about this:

import pytesseract
from pdf2image import convert_from_path
import pdfplumber
from fpdf import FPDF

# Perform OCR on the image to extract text
ocr_text = pytesseract.image_to_string(image)

# Extract text from the original PDF
pdf_path_original = "/mnt/data/KA Networks.pdf"

# Create a new PDF for the annotated version
class PDF(FPDF):
    def header(self):
        self.set_font('Arial', 'B', 12)
        self.cell(0, 10, 'On the Realization of a Kolmogorov Network', 0, 1, 'C')

    def chapter_title(self, title):
        self.set_font('Arial', 'B', 12)
        self.cell(0, 10, title, 0, 1, 'L')
        self.ln(10)

    def chapter_body(self, body):
        self.set_font('Arial', '', 12)
        self.multi_cell(0, 10, body)
        self.ln()

# Initialize PDF
pdf = PDF()
pdf.add_page()

# Add OCR text to the new PDF
pdf.chapter_title('OCR Extracted Text')
pdf.chapter_body(ocr_text)

# Save the PDF
pdf_path_annotated = "/mnt/data/Kolmogorov_Network_Annotated.pdf"
pdf.output(pdf_path_annotated)

pdf_path_annotated



# Define a function to clean the OCR text for any unsupported characters
def clean_text(text):
    return text.encode("latin1", "replace").decode("latin1")

# Clean the OCR text
cleaned_ocr_text = clean_text(ocr_text)

# Initialize PDF with cleaned text
pdf = PDF()
pdf.add_page()

# Add cleaned OCR text to the new PDF
pdf.chapter_title('OCR Extracted Text')
pdf.chapter_body(cleaned_ocr_text)

# Save the PDF
pdf_path_annotated_cleaned = "/mnt/data/Kolmogorov_Network_Annotated_Cleaned.pdf"
pdf.output(pdf_path_annotated_cleaned)

pdf_path_annotated_cleaned

#didn't do a great job with equations.
# Turns text (like a pickup code or a link) into a QR code picture.
import io  # in-memory "files", so we don't have to save anything to disk
import re  # regular expressions, used below to tidy up the SVG size
import qrcode  # the library that actually draws QR codes
import qrcode.image.svg  # its SVG (vector picture) output


def qr_svg(text: str) -> str:
    """An SVG you can drop straight into a page (pure Python, no image libraries needed)."""
    # Draw the QR code as an SVG; box_size is how big each square is and border is the empty margin around it.
    qr_image = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    # Save the picture into memory instead of a file.
    buffer = io.BytesIO()
    qr_image.save(buffer)
    # Read the saved bytes back out as text.
    svg_text = buffer.getvalue().decode()
    # Cut off anything before the <svg tag (like the XML header) so it can be pasted straight into HTML.
    svg_text = svg_text[svg_text.index("<svg"):]
    # Swap the fixed millimetre size for 100% so the QR scales to whatever box it's put in.
    return re.sub(r' width="[^"]*mm" height="[^"]*mm"', ' width="100%" height="100%"', svg_text, count=1)

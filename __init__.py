"""Cartel: golf group scoring, quotas and money."""
import logging

__version__ = "1.1.0"

# pdfminer logs "Could not get FontBBox from font descriptor" at WARNING every
# time a page object is touched, because one font in the club's tee sheet omits
# an optional bounding box. It has no effect on parsing - the geometric block
# detection reads rects and words, not font metrics - but the new parser reads
# each page several times across two pages, so it floods the console and buries
# anything that actually matters.
#
# Errors still come through; only the cosmetic warnings are suppressed.
logging.getLogger("pdfminer").setLevel(logging.ERROR)
logging.getLogger("pdfplumber").setLevel(logging.ERROR)

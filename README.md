# receipt-parse

Parse Swedish store receipts (PDF or text) into structured JSON/CSV.

```
python3 receipt-parse.py receipt.pdf [--out-dir parsed/] [--csv items.csv]
```

## Why

Tracking spend from paper receipts is tedious. This extracts line items
from Swedish receipts (ICA, Coop, Hemköp, etc.) into structured data you
can feed into a budget or expense tracker.

## What it extracts

- Store, date, total
- Line items: product name, quantity, price
- VAT/category hints where present

## Requirements

- Python 3.8+
- For PDF input: `pdfplumber` (`pip install pdfplumber`)
- Text input works with zero dependencies

## Output

- `--out-dir`: per-receipt JSON files
- `--csv`: merged line items in one CSV

## Links

Part of the [vhsgreed](https://vhsgreed.win) toolset: data, code, and methods in the open.
